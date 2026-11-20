import csv
import subprocess
from collections.abc import Callable, Sequence
from functools import wraps
from typing import Any, Literal

from .basefuc import DefaultInstance, DelegatedBase, ensure_param, lazy_import, module
from .charfuc import CharFuc
from .timefuc import TimeFuc

# ============================== 延迟加载依赖 ==============================

win32gui = lazy_import("win32gui", pip_name="pywin32")
win32con = lazy_import("win32con", pip_name="pywin32")
pyautogui = lazy_import("pyautogui")


# ============================== 独立函数工具 ==============================

def log_runtime(enabled: bool = True) -> Callable:
    """
    装饰器：记录函数运行时间。

    :param enabled: 是否启用耗时度量
    :return: 装饰器闭包
    """

    def _resolve_qualname(func: Callable, first_arg: Any = None) -> str:
        """
        解析并构建函数的完整调用层级名称（支持子模块向顶层宿主反向追溯）。

        :param func: 目标函数对象
        :param first_arg: 运行时的首个位置参数（通常为 self 或 cls）
        :return: 格式化后的完整函数名称（如 'CharFuc.process.convert_numbers'）
        """
        raw_qualname = getattr(func, "__qualname__", func.__name__)

        if first_arg is not None and hasattr(first_arg, "_parent"):  # 若首个参数属于 DelegatedBase 子模块实例，沿 _parent 链路逆向解析出公开路径
            parts: list[str] = [func.__name__]
            curr = first_arg

            while hasattr(curr, "_parent"):
                # noinspection PyProtectedMember
                parent = curr._parent
                module_name = curr.__class__.__name__.lstrip("_").lower()  # 去除下划线并转小写
                parts.append(module_name)
                curr = parent

            parts.append(curr.__class__.__name__)  # 追加根宿主门面类名
            parts.reverse()
            return ".".join(parts)

        return raw_qualname  # 其余普通类方法、静态函数或独立函数，直接使用原生保留类层级的 __qualname__

    def decorator(func: Callable) -> Callable:
        """
        装饰器构建层：接收并包装目标函数。

        :param func: 被装饰的目标函数
        :return: 具备耗时度量与打印能力的包装函数
        """

        @wraps(func)
        def wrapper(*args, **kwargs):
            """
            运行时拦截层：在目标函数执行前后度量单调时钟时间差并输出日志。

            :param args: 位置参数
            :param kwargs: 关键字参数
            :return: 目标函数执行返回值
            """
            if not enabled:  # 未启用则直接执行，不计时
                return func(*args, **kwargs)

            start, _ = TimeFuc.measure_runtime()
            result = func(*args, **kwargs)
            _, elapsed = TimeFuc.measure_runtime(start)

            first_arg = args[0] if args else None
            func_name = _resolve_qualname(func, first_arg)
            print(f"Function '{func_name}' executed in {elapsed:.4f}s")  # 标准英文耗时日志

            return result

        return wrapper

    return decorator


def subprocess_run(
        command: Sequence[str] | str,
        async_mode: bool = False,
        stdout: int | None = None,
        stderr: int | None = None,
        encoding: str | None = "utf-8",
        errors: Literal["strict", "ignore", "replace", "surrogateescape"] | None = "replace",
        shell: bool = False,
        cwd: str | None = None,
        **kwargs: Any,
) -> subprocess.CompletedProcess | subprocess.Popen:
    """
    执行系统命令并返回结果或进程对象。

    :param command: 要执行的命令字符串或参数序列
    :param async_mode: 是否异步执行命令（False: 同步阻塞, True: 异步非阻塞）
    :param stdout: 指定子进程标准输出流的处理方式（同步默认 PIPE，异步默认 DEVNULL）
    :param stderr: 指定子进程标准错误流的处理方式（同步默认 PIPE，异步默认 DEVNULL）
    :param encoding: 子进程输出的文本解码字符集
    :param errors: 文本解码异常处理策略（默认为 "replace"，避免非法字符导致崩溃）
    :param shell: 是否通过系统 Shell 环境执行命令
    :param cwd: 指定子进程的工作目录路径
    :param kwargs: 透传给 subprocess 底层的额外关键字参数（如 creationflags, env 等）
    :return: CompletedProcess（同步）或 Popen（异步）对象
    """
    if stdout is None:  # 异步模式下若未指定流，默认使用 DEVNULL 丢弃输出，防止管道缓冲区占满导致进程挂起
        stdout = subprocess.DEVNULL if async_mode else subprocess.PIPE
    if stderr is None:
        stderr = subprocess.DEVNULL if async_mode else subprocess.PIPE

    # https://www.cnblogs.com/superbaby11/p/16195273.html
    executor = subprocess.Popen if async_mode else subprocess.run  # 异步非阻塞执行 或 同步阻塞执行

    return executor(
        command,
        stdout=stdout,
        stderr=stderr,
        encoding=encoding,
        errors=errors,
        shell=shell,
        cwd=cwd,
        **kwargs,
    )


# ============================== 核心门面与子模块 ==============================

class SysFuc(DefaultInstance):
    """
    系统功能封装类。

    子模块：
        windows   → 窗口控制
        input     → 键盘与输入控制
        command   → 系统命令与进程
    """

    windows: "_Windows"  # IDE 静态类型提示
    input: "_Input"
    command: "_Command"

    windows = module(lambda parent: _Windows(parent))  # 模块代理挂载
    input = module(lambda parent: _Input(parent))
    command = module(lambda parent: _Command(parent))

    def __init__(self, is_print: bool = False) -> None:
        """
        初始化系统功能模块。

        :param is_print: 是否启用枚举与过程调试打印
        """
        self._is_print = is_print


class _Windows(DelegatedBase[SysFuc]):
    """Windows 窗口操作模块"""

    @staticmethod
    def _normalize_hwnd(hwnd: int | str) -> int:
        """
        将窗口句柄统一校验并转换为整数。

        :param hwnd: 窗口句柄
        :return: int 整数句柄
        :raises ValueError: 当句柄非法时抛出
        """
        if not CharFuc.detect.is_number(hwnd):
            raise ValueError(f"Invalid window handle: {hwnd!r}")

        return int(hwnd)

    @staticmethod
    def _ensure_hwnd(param: str = "hwnd") -> Callable:
        """
        自动将指定位置或关键字参数转换为合法整数句柄的装饰器。

        :param param: 待拦截转换的形参名（默认 'hwnd'）
        :return: 装饰器
        """
        return ensure_param(param, lambda x: _Windows._normalize_hwnd(x))

    def enum(self) -> dict[int, str]:
        """
        枚举当前系统所有可见且可用的顶层窗口。

        :return: {窗口句柄: 窗口标题} 映射字典
        """
        windows: dict[int, str] = {}

        def callback(hwnd: int, _) -> None:
            """
            枚举窗口底层回调：过滤不可见及无标题窗口并登记有效信息。

            :param hwnd: 当前遍历到的顶层窗口句柄
            :param _: Win32 传递的预留上下文数值（未使用）
            """
            if not (
                    win32gui.IsWindow(hwnd)
                    and win32gui.IsWindowEnabled(hwnd)
                    and win32gui.IsWindowVisible(hwnd)
            ):
                return

            if title := win32gui.GetWindowText(hwnd):  # 过滤空白标题窗口
                windows[hwnd] = title  # 存入字典

        win32gui.EnumWindows(callback, 0)

        if getattr(self._parent, "_is_print", False):
            print("\n".join([f"Hwnd: {hwnd}, Title: {title}" for hwnd, title in windows.items()]))

        return windows

    def find(self, keyword: str | int, by: Literal["h", "hwnd", "t", "title"] = "h") -> int | str | None:
        """
        根据句柄获取标题，或根据标题模糊查找窗口句柄。

        :param keyword: 待查询的标题子串或句柄
        :param by: 查找模式：
                - 'h' / 'hwnd'  : 根据句柄查标题；
                - 't' / 'title' : 根据标题查句柄。
        :return: 匹配到的结果；未找到返回 None
        :raises ValueError: 当查询模式不支持时抛出
        """
        windows = self.enum()
        mode = by.lower()

        if mode in ("h", "hwnd"):
            return windows.get(self._normalize_hwnd(keyword), None)

        if mode in ("t", "title"):
            search_str = str(keyword)
            return next(
                (hwnd for hwnd, title in windows.items() if search_str in title),
                None,
            )

        raise ValueError(f"Invalid lookup mode: {by!r}. Expected 'h' or 't'")

    @_ensure_hwnd()
    def close(self, hwnd: int | str) -> None:
        """
        向指定窗口发送异步关闭消息。

        :param hwnd: 窗口句柄
        """
        win32gui.PostMessage(int(hwnd), win32con.WM_CLOSE, 0, 0)

    @_ensure_hwnd()
    def exists(self, hwnd: int | str) -> bool:
        """
        判断指定窗口是否存在。

        :param hwnd: 窗口句柄
        :return: 存在返回 True，否则返回 False
        """
        return bool(win32gui.IsWindow(int(hwnd)))

    @_ensure_hwnd()
    def foreground(self, hwnd: int | str, retries: int = 5, interval: float = 0.1, temporary: bool = True) -> None:
        """
        将窗口激活置顶并抢占前台焦点。

        :param hwnd: 窗口句柄
        :param retries: 最大重试抢占次数
        :param interval: 每次重试等待间隔（秒）
        :param temporary: 获得焦点后是否恢复为普通无置顶状态
        """
        target_hwnd = int(hwnd)

        for _ in range(retries):
            self.minimize(target_hwnd)  # 先最小化再还原，显著提升 Windows 下焦点争抢成功率
            self.show(target_hwnd)

            win32gui.SetWindowPos(
                target_hwnd,
                win32con.HWND_TOPMOST,
                0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
            )

            TimeFuc.sleep_seconds(interval)

            if win32gui.GetForegroundWindow() == target_hwnd:  # 焦点抢占成功
                if temporary:  # 取消置顶锁定，避免长期遮挡
                    win32gui.SetWindowPos(
                        target_hwnd,
                        win32con.HWND_NOTOPMOST,
                        0, 0, 0, 0,
                        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                    )
                return

    @_ensure_hwnd()
    def hide(self, hwnd: int | str) -> None:
        """
        隐藏指定窗口。

        :param hwnd: 窗口句柄
        """
        win32gui.ShowWindow(int(hwnd), win32con.SW_HIDE)

    @_ensure_hwnd()
    def maximize(self, hwnd: int | str) -> None:
        """
        最大化指定窗口。

        :param hwnd: 窗口句柄
        """
        win32gui.ShowWindow(int(hwnd), win32con.SW_MAXIMIZE)

    @_ensure_hwnd()
    def minimize(self, hwnd: int | str) -> None:
        """
        最小化指定窗口。

        :param hwnd: 窗口句柄
        """
        win32gui.ShowWindow(int(hwnd), win32con.SW_MINIMIZE)

    @_ensure_hwnd()
    def rect(self, hwnd: int | str) -> tuple[int, int, int, int]:
        """
        获取窗口在屏幕上的坐标边界。

        :param hwnd: 窗口句柄
        :return: 四元组 (left, top, right, bottom)
        """
        return win32gui.GetWindowRect(int(hwnd))

    @_ensure_hwnd()
    def show(self, hwnd: int | str) -> None:
        """
        将窗口正常显示还原。

        :param hwnd: 窗口句柄
        """
        win32gui.ShowWindow(int(hwnd), win32con.SW_SHOWNORMAL)

    @_ensure_hwnd()
    def visible(self, hwnd: int | str) -> bool:
        """
        判断指定窗口当前是否可见。

        :param hwnd: 窗口句柄
        :return: 可见返回 True，隐藏返回 False
        """
        return bool(win32gui.IsWindowVisible(int(hwnd)))


class _Input(DelegatedBase[SysFuc]):
    """键盘与输入控制模块"""

    @classmethod
    def down(cls, key: str) -> None:
        """
        按下按键但不释放。

        :param key: 按键键名
        """
        pyautogui.keyDown(key)

    @classmethod
    def up(cls, key: str) -> None:
        """
        释放指定按键。

        :param key: 按键键名
        """
        pyautogui.keyUp(key)

    @classmethod
    def press(
            cls,
            *keys: str | Sequence[str],
            ctrl: bool = False,
            alt: bool = False,
            shift: bool = False,
            interval: float = 0.0,
    ) -> None:
        """
        执行单次按键或组合快捷键操作。

        :param keys: 按键名称序列（支持平铺传参或嵌套列表）
        :param ctrl: 是否按下 Ctrl 修饰键
        :param alt: 是否按下 Alt 修饰键
        :param shift: 是否按下 Shift 修饰键
        :param interval: 多键逐个输入时的步进间隔（秒）
        """
        expanded: list[str] = []  # 平铺可能传入的嵌套序列
        for item in keys:
            if isinstance(item, (tuple, list)):
                expanded.extend(str(k) for k in item)
            else:
                expanded.append(str(item))

        if not expanded:
            return

        modifiers = [
            k for k, enabled in (
                ("ctrl", ctrl),
                ("alt", alt),
                ("shift", shift)
            ) if enabled
        ]  # 收集处于激活状态的修饰键

        if interval <= 0:  # 一次性发送热键组合情景
            if modifiers or len(expanded) > 1:
                pyautogui.hotkey(*modifiers, *expanded)
            else:
                pyautogui.press(expanded[0])
            return

        for key in expanded:  # 逐键敲击情景（附带间隔）
            if modifiers:
                pyautogui.hotkey(*modifiers, key)
            else:
                pyautogui.press(key)

            TimeFuc.sleep_seconds(interval)

    @classmethod
    def write(cls, text: str, interval: float = 0.0, newline: bool = False) -> None:
        """
        模拟键入文本内容（自动兼容中文及 Unicode 字符输入）。

        当检测到非 ASCII 字符（如中文）时，自动采用剪贴板粘贴机制，规避 pyautogui 字符截断问题。

        :param text: 待输入的文本内容
        :param interval: 单字符键入间隔（秒，对纯 ASCII 生效）
        :param newline: 输入完毕后是否自动发送回车键
        """
        if text.isascii():
            pyautogui.write(text, interval=interval)
        else:  # 中文及复合 Unicode：先写入剪贴板再触发 Ctrl+V 粘贴
            CharFuc.clipboard.write(text)
            cls.press("v", ctrl=True)

        if newline:
            cls.press("enter")


class _Command(DelegatedBase[SysFuc]):
    """系统命令与进程管理模块"""

    @staticmethod
    def enum() -> dict[int, str]:
        """
        获取当前系统所有运行中的进程清单。

        采用 CSV 输出规避进程名中包含空格导致的切分截断。

        :return: {PID: 进程名称} 字典映射
        """
        res = subprocess_run(
            ["tasklist"],
            async_mode=False,
            encoding="gbk"
        )  # Windows 下 tasklist 默认输出 GBK

        processes: dict[int, str] = {}
        if not isinstance(res, subprocess.CompletedProcess) or not res.stdout:
            return processes

        reader = csv.reader(res.stdout.splitlines())
        for row in reader:
            if len(row) >= 2 and row[1].isdigit():  # tasklist CSV 列结构：[映像名称, PID, 会话名, 会话#, 内存使用]
                processes[int(row[1])] = row[0]

        return processes

    @classmethod
    def find(cls, keyword: str | int, by: Literal["p", "pid", "n", "name"] = "p") -> int | str | None:
        """
        根据 PID 查询进程名，或根据进程名查询对应 PID。

        :param keyword: 待查询的 PID 数字或进程名字符串（忽略大小写）
        :param by: 查询方式：
                - 'p' / 'pid'  : 根据 PID 查进程名；
                - 'n' / 'name' : 根据进程名查 PID。
        :return: 查询结果；未找到返回 None
        :raises ValueError: 当查询模式不支持时抛出
        """
        processes = cls.enum()
        mode = by.lower()

        if mode in ("p", "pid"):
            target_pid = int(keyword)
            return processes.get(target_pid, None)

        if mode in ("n", "name"):
            target_name = str(keyword).lower()
            return next(
                (pid for pid, name in processes.items() if name.lower() == target_name),
                None
            )

        raise ValueError(f"Invalid lookup mode: {by!r}. Expected 'p' or 'n'")

    @classmethod
    def exists(cls, pid: int) -> bool:
        """
        判断指定 PID 的系统进程是否存在。

        :param pid: 进程 ID
        :return: 存在返回 True，否则返回 False
        """
        return pid in cls.enum()

    @staticmethod
    def kill(pid: int, force: bool = True) -> subprocess.CompletedProcess:
        """
        终止指定 PID 的进程树。

        :param pid: 目标进程 ID
        :param force: 是否强制终止（/F 参数）
        :return: taskkill 子进程执行结果
        """
        cmd = ["taskkill", "/T", "/PID", str(pid)]
        if force:
            cmd.insert(1, "/F")

        res = subprocess_run(
            cmd,
            async_mode=False,
            encoding="gbk"
        )
        return res

    @staticmethod
    def port_pid(port: int) -> int | None:
        """
        获取当前监听指定本地网络端口的进程 PID。

        :param port: 本地端口号
        :return: 对应的 PID；若未被监听则返回 None
        """
        res = subprocess_run(
            ["netstat", "-ano"],
            async_mode=False,
            encoding="gbk"
        )

        if not isinstance(res, subprocess.CompletedProcess) or not res.stdout:
            return None

        target_port_str = str(port)
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5:  # netstat -ano 行格式：Proto  Local Address   Foreign Address   State   PID
                continue

            local_address = parts[1]
            state = parts[3].upper()
            pid_str = parts[4]

            port_part = local_address.rsplit(":", 1)[-1]  # 严格提取冒号后的端口号，兼容 IPv4 与 [::]:80 格式

            if (
                    port_part == target_port_str
                    and state == "LISTENING"
                    and pid_str.isdigit()
            ):
                return int(pid_str)

        return None
