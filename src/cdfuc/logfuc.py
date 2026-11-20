import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from .basefuc import ClassProxy, DefaultInstance


# ============================== 内部处理器与桥接 ==============================

class _UIBridgeHandler(logging.Handler):
    """
    UI 事件总线桥接处理器。

    继承自标准 logging.Handler，负责将捕获到的 LogRecord 记录安全投递至外部注册的回调函数（如 Qt 信号发射器）。
    """

    def __init__(self, callback: Callable[[str, str], None] | None = None) -> None:
        """
        初始化 UI 桥接处理器。

        :param callback: 初始日志回调函数，签名要求为 callback(formatted_msg, level)
        """
        super().__init__()

        self._callback: Callable[[str, str], None] | None = callback

    def set_callback(self, callback: Callable[[str, str], None] | None) -> None:
        """
        动态设置或替换全局回调监听器。

        :param callback: 目标回调函数，传入 None 则注销回调
        """
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        """
        处理并分发单条日志记录。

        自动调用 Handler.format() 生成包含时间戳、模块名及完整 exc_info
        多行堆栈的最终文本，并隔离回调内部抛出的异常以防日志管道中断。

        :param record: 待处理的标准 logging.LogRecord 实例
        """
        try:
            formatted_msg = self.format(record)  # format() 内部会自动检测 record.exc_info 并追加 formatException 生成的完整堆栈文本
            if self._callback is not None:
                self._callback(formatted_msg, record.levelname)
        except (ValueError, TypeError, KeyError, RuntimeError):  # 委托 logging 内部标准错误处理器输出排查信息，不阻断主线程
            self.handleError(record)


# ============================== 核心门面类 ==============================

class LogFuc(DefaultInstance):
    """
    通用日志管理与事件总线门面类。

    基于 Python 标准 logging 体系封装，无缝契合 DefaultInstance 契约（同时支持单例实例调用与类代理直接调用）。
    具备本地文件轮转准备、控制台流输出、子进程命令行流捕获及面向 GUI/Qt 的跨线程事件分发能力。
    """

    _DEFAULT_FMT: str = "%(asctime)s %(levelname)s: %(message)s"  # 默认标准输出格式：时间 | 级别 | 来源命名空间 | 日志消息 (异常时自动在下一行追加 Traceback)
    _DEFAULT_DATE_FMT: str = "%Y-%m-%d %H:%M:%S"

    def __init__(
            self,
            name: str = "cdfuc",
            level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO",
            log_path: str | Path | None = None,
            console: bool = True,
    ) -> None:
        """
        初始化日志门面实例。

        :param name: 日志记录器命名空间（区分日志来源）
        :param level: 初始日志过滤门限，可选 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
        :param log_path: 本地落盘日志文件绝对或相对路径，为 None 则不落盘
        :param console: 是否开启标准控制台（sys.stdout）流式打印
        """
        self.name: str = name
        self._listeners: list[Callable[[str, str], None]] = []

        # ------------------------------ Logger 核心实例装配 ------------------------------

        self._logger: logging.Logger = logging.getLogger(name)
        for handler in tuple(self._logger.handlers):  # 同名日志器重建时移除旧实例管理的处理器，防止回调与文件句柄泄漏
            if getattr(handler, "_cdfuc_managed", False):
                self._logger.removeHandler(handler)
                handler.close()

        if not any(isinstance(handler, logging.NullHandler) for handler in self._logger.handlers):
            self._logger.addHandler(logging.NullHandler())  # 避免第三方宿主未配置处理器时出现无处理器警告

        self._formatter: logging.Formatter = logging.Formatter(
            fmt=self._DEFAULT_FMT,
            datefmt=self._DEFAULT_DATE_FMT,
        )  # 统一格式化器

        # ------------------------------ UI 桥接处理器装配 ------------------------------

        self._ui_handler: _UIBridgeHandler = _UIBridgeHandler(self._dispatch_listeners)
        self._ui_handler._cdfuc_managed = True
        self._ui_handler.setFormatter(self._formatter)
        self._logger.addHandler(self._ui_handler)

        self._file_handler: logging.FileHandler | None = None  # 处理器生命周期持有槽（防止动态重配时泄漏或重复添加）
        self._stream_handler: logging.StreamHandler | None = None

        self.setup(level=level, log_path=log_path, console=console)  # 执行初始配置装载

    # ============================== 内部工具 ==============================

    def _dispatch_listeners(self, formatted_msg: str, level: str) -> None:
        """
        向所有外部注册的监听器广播格式化后的日志消息。

        内部独立捕获每个监听器的异常，避免个别 UI 组件渲染异常波及其他监听器或打崩日志主链路。

        :param formatted_msg: 经过 Formatter 处理后的完整日志字符串
        :param level: 大写日志级别字符串（如 'INFO', 'ERROR'）
        """
        for callback in tuple(self._listeners):  # 回调中注册或注销监听器时，不影响当前轮广播
            try:
                callback(formatted_msg, level)
            except RuntimeError:  # 常见场景：Qt 组件已在主线程关闭销毁，属于界面生命周期正常结束，安全静默跳过
                pass
            except (TypeError, AttributeError) as err:  # 外部注册的回调签名不匹配 (如入参缺少 level) 或访问了未初始化的属性，输出诊断警示，避免开发期排查困难
                cb_name = getattr(callback, "__name__", repr(callback))
                sys.stderr.write(f"[_dispatch_listeners:{cb_name}] Callback contract error: {err}\n")
            except Exception as err:  # 隔离其他监听器异常，避免日志回调中断宿主业务
                cb_name = getattr(callback, "__name__", repr(callback))
                sys.stderr.write(f"[_dispatch_listeners:{cb_name}] Callback failed: {err}\n")

    # ============================== 基础配置与生命周期 ==============================

    @ClassProxy
    def setup(
            self,
            level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO",
            log_path: str | Path | None = None,
            console: bool = True,
    ) -> None:
        """
        动态注入或刷新全局配置。

        专供宿主应用在完成配置模块（如 config.py）加载后，在生命周期早期一次性重塑日志规则。
        支持幂等调用，能安全关闭旧的文件句柄并根据新路径重建。

        :param level: 日志过滤级别
        :param log_path: 新的日志文件路径，传入 None 表示注销文件输出
        :param console: 是否保持或开启终端控制台输出
        """
        log_level = getattr(logging, level.upper(), logging.INFO)
        self._logger.setLevel(log_level)
        self._ui_handler.setLevel(log_level)

        # ------------------------------ 1. 控制台输出处理器生命周期维护 ------------------------------

        if console:
            if self._stream_handler is None:
                self._stream_handler = logging.StreamHandler(sys.stdout)
                self._stream_handler._cdfuc_managed = True
                self._stream_handler.setFormatter(self._formatter)
                self._logger.addHandler(self._stream_handler)
            self._stream_handler.setLevel(log_level)
        elif self._stream_handler is not None:
            self._logger.removeHandler(self._stream_handler)
            self._stream_handler = None

        # ------------------------------ # 2. 文件落盘处理器生命周期维护（支持热重载路径） ------------------------------

        if log_path:
            p = Path(log_path)
            p.parent.mkdir(parents=True, exist_ok=True)

            if self._file_handler is not None:  # 若原先已存在文件处理器，安全注销并显式关闭文件句柄
                self._logger.removeHandler(self._file_handler)
                self._file_handler.close()

            self._file_handler = logging.FileHandler(str(p), encoding="utf-8")
            self._file_handler._cdfuc_managed = True
            self._file_handler.setFormatter(self._formatter)
            self._file_handler.setLevel(log_level)
            self._logger.addHandler(self._file_handler)
        elif self._file_handler is not None:
            self._logger.removeHandler(self._file_handler)
            self._file_handler.close()
            self._file_handler = None

        self._logger.propagate = False  # 禁用向根节点冒泡，确保输出仅由当前实例管理的 Handlers 严格控制

    # ============================== 监听器订阅管理 ==============================

    @ClassProxy
    def add_listener(self, callback: Callable[[str, str], None]) -> None:
        """
        注册外部监听器。

        常用于上层应用挂载 Qt 信号发射、状态栏实时滚动更新或前端控制台面板呈现。
        自动去重，确保同一回调不会被重复绑定。

        :param callback: 接收消息的回调函数，形如 callback(formatted_msg: str, level: str)
        """
        if callback not in self._listeners:
            self._listeners.append(callback)

    @ClassProxy
    def remove_listener(self, callback: Callable[[str, str], None]) -> None:
        """
        注销指定的外部监听器。

        :param callback: 待解绑的回调函数对象
        """
        if callback in self._listeners:
            self._listeners.remove(callback)

    # ============================== CMD 子进程流捕获接口 ==============================

    @ClassProxy
    def stream(self, line: str, level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "DEBUG") -> None:
        """
        捕获并分发外部命令行（cmd/子进程）管道吐出的单行流式文本。

        专为 subprocess 管道实时捕获设计。自动剔除尾部换行符并作为指定级别推入分发管道，
        供 UIBridgeHandler 驱动进度条解析及落盘记录。

        :param line: 管道读取到的原始单行字符串（可带 \\r 或 \\n）
        :param level: 指定输出的日志级别，默认 'DEBUG'
        """
        clean_text = line.rstrip("\r\n")
        if clean_text:
            self.log(clean_text, level=level)

    # ============================== 标准日志记录接口 ==============================

    @ClassProxy
    def log(
            self,
            message: str,
            level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO",
    ) -> None:
        """
        通用日志记录核心入口。

        :param message: 日志消息正文
        :param level: 严重等级，不区分大小写，默认 'INFO'
        """
        lvl_num = getattr(logging, level.upper(), logging.INFO)
        self._logger.log(lvl_num, message)

    @ClassProxy
    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """
        记录 DEBUG 级别的低粒度调试信息。

        :param msg: 消息模板字符串
        :param args: 格式化参数位置参数
        :param kwargs: 透传给标准 logger 的关键字参数（如 extra 等）
        """
        self._logger.debug(msg, *args, **kwargs)

    @ClassProxy
    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """
        记录 INFO 级别的常规运行事件。

        :param msg: 消息模板字符串
        :param args: 格式化参数位置参数
        :param kwargs: 透传给标准 logger 的关键字参数
        """
        self._logger.info(msg, *args, **kwargs)

    @ClassProxy
    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """
        记录 WARNING 级别的潜在隐患或非破坏性异常。

        :param msg: 消息模板字符串
        :param args: 格式化参数位置参数
        :param kwargs: 透传给标准 logger 的关键字参数
        """
        self._logger.warning(msg, *args, **kwargs)

    @ClassProxy
    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """
        记录 ERROR 级别的破坏性错误或业务失败信息。

        :param msg: 消息模板字符串
        :param args: 格式化参数位置参数
        :param kwargs: 透传给标准 logger 的关键字参数（包含 exc_info 等）
        """
        self._logger.error(msg, *args, **kwargs)

    @ClassProxy
    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """
        记录 ERROR 级别错误，并强制提取当前运行栈的完整 Traceback 堆栈信息。

        必须且仅应在 except 语句块中调用。

        :param msg: 异常概括描述
        :param args: 格式化参数位置参数
        :param kwargs: 透传给标准 logger 的关键字参数
        """
        self._logger.exception(msg, *args, **kwargs)

    @ClassProxy
    def critical(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """
        记录 CRITICAL 级别的系统崩溃或阻断性致命错误。

        :param msg: 消息模板字符串
        :param args: 格式化参数位置参数
        :param kwargs: 透传给标准 logger 的关键字参数
        """
        self._logger.critical(msg, *args, **kwargs)
