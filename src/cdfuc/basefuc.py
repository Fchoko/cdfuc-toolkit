import importlib
import threading

from collections.abc import Callable, Sequence
from functools import update_wrapper, wraps
from inspect import Parameter, signature
from pathlib import Path
from typing import (
    Any, Generic, Literal, ParamSpec, Self, TypeVar, cast, overload,
)

# ============================== 泛型定义 ==============================

T = TypeVar("T")  # 父级宿主对象类型
M = TypeVar("M")  # 绑定的子模块类型
P = ParamSpec("P")  # 被代理函数的入参规范
R = TypeVar("R")  # 被代理函数的返回值类型


# ============================== 默认实例管理 ==============================

class DefaultInstance:
    """
    默认单例管理器。

    通过类直接访问方法或属性时自动创建并复用默认实例。
    采用类型注册表与双重检查锁，保证多线程安全并防止子类间状态踩踏。
    """

    _instances: dict[type, Any] = {}
    _lock = threading.Lock()

    @classmethod
    def _get_default_instance(cls) -> Self:
        """
        获取当前类的默认单例对象（采用双重检查锁机制实现懒加载）。

        :return: 线程安全的类默认单例实例
        """
        if cls not in cls._instances:
            with cls._lock:
                if cls not in cls._instances:
                    cls._instances[cls] = cls()

        return cls._instances[cls]


# ============================== 统一代理描述符 ==============================

class InstanceProxyBase:
    """
    代理描述符抽象基类。

    统一解析访问者身份：通过类调用时自动锚定到所属类的默认单例，通过实例调用时直接返回该实例自身。
    """

    @staticmethod
    def _resolve_obj(obj: Any, owner: type[DefaultInstance]) -> DefaultInstance:
        """
        解析当前访问对象，确保始终返回有效的宿主实例。

        :param obj: 当前调用的实例对象；若通过类直接访问则为 None
        :param owner: 挂载该描述符的目标类（必须继承自 DefaultInstance）
        :return: 绑定的有效实例对象
        """
        if obj is None:
            if not hasattr(owner, "_get_default_instance"):
                raise TypeError(f"{owner} must implement _get_default_instance")
            # noinspection PyProtectedMember
            obj = owner._get_default_instance()  # 抑制报错指令

        return obj


# ============================== 模块代理 ==============================

class ModuleProxy(InstanceProxyBase, Generic[M]):
    """
    子模块代理描述符。

    支持通过类或实例直接链式调用子模块，并在宿主内部执行懒加载缓存。
    """

    def __init__(self, module_type: type[M] | Callable[[Any], M]) -> None:
        """
        初始化模块代理描述符。

        :param module_type: 子模块类类型或接收宿主实例并返回子模块的工厂函数
        """
        self._module_type = module_type
        self._name: str | None = None

    def __set_name__(self, owner: type[DefaultInstance], name: str) -> None:
        """
        自动捕获描述符在宿主类中的属性名，消除手动传参风险。

        :param owner: 拥有该描述符的宿主类
        :param name: 属性在宿主类定义中的变量名
        """
        self._name = name

    def __get__(self, obj: Any, owner: type[DefaultInstance]) -> M:
        """
        访问子模块实例，未初始化时自动创建并缓存至宿主实例字典。

        :param obj: 当前访问的宿主实例；通过类访问时为 None
        :param owner: 拥有该描述符的宿主类
        :return: 对应的子模块实例
        """
        obj = self._resolve_obj(obj, owner)
        if self._name not in obj.__dict__:
            obj.__dict__[self._name] = self._module_type(obj)

        return obj.__dict__[self._name]


def module(module_type: type[M] | Callable[[Any], M]) -> ModuleProxy[M]:
    """
    构建子模块代理描述符的工厂函数。

    :param module_type: 目标子模块类型或构造回调函数
    :return: 实例化后的 ModuleProxy 描述符对象
    """
    return ModuleProxy(module_type)


# ============================== 方法代理 ==============================

class ClassProxy(InstanceProxyBase, Generic[P, R]):
    """
    实例方法类级代理描述符。

    赋予普通实例方法直接通过类名调用的能力，并完整透传 IDE 静态分析类型签名。
    """

    def __init__(self, func: Callable[P, R]) -> None:
        """
        初始化方法代理描述符。

        :param func: 被包装的原生实例方法
        """
        self.func = func
        update_wrapper(cast(Any, self), func)

    # -------------------- 静态类型重载声明（运行时不执行） --------------------

    @overload
    def __get__(self, obj: None, owner: type[DefaultInstance]) -> Callable[P, R]:
        """
        处理通过类直接访问的类型推导场景（如 ArchFuc.compress）。

        当通过类调用描述符时，传入的 obj 固定为 None；
        该声明告知静态分析器此时返回的是与原函数签名一致的绑定可调用对象，避免误报缺少 self 参数。

        :param obj: 静态类型固定为 None（表示由类对象发起调用）
        :param owner: 挂载该方法的宿主类对象
        :return: 具备完整参数签名 P 与返回值 R 的可调用对象
        """
        ...

    @overload
    def __get__(self, obj: Any, owner: type[DefaultInstance]) -> Callable[P, R]:
        """
        处理通过具体实例访问的类型推导场景（如 ArchFuc().compress）。

        当通过常规实例调用描述符时，传入的 obj 为该实例对象；
        该声明告知静态分析器此时返回正常绑定到该实例的可调用对象。

        :param obj: 触发调用的具体实例对象
        :param owner: 挂载该方法的宿主类对象
        :return: 具备完整参数签名 P 与返回值 R 的可调用对象
        """
        ...

    # -------------------- 描述符底层实际绑定逻辑（运行时真正执行） --------------------

    def __get__(self, obj: Any, owner: type[DefaultInstance]) -> Callable[P, R]:
        """
        执行描述符属性绑定并返回绑定实例的方法对象。

        无论外部是通过类直接访问还是通过既有实例访问，均统一解析出有效的目标实例，
        并调用原生函数底层的 __get__ 协议将实例注入为第一个位置参数（self）。

        :param obj: 当前访问的宿主实例；通过类访问时为 None
        :param owner: 拥有该方法的宿主类
        :return: 绑定至宿主实例的方法对象
        """
        target = self._resolve_obj(obj, owner)
        return self.func.__get__(target, owner)  # type: ignore[union-attr]


# ============================== 子模块基类 ==============================

class DelegatedBase(Generic[T]):
    """
    子模块委托基类。

    子模块未定义的公开属性自动向上穿透至父级宿主，同时严格隔离内部私有变量。
    """

    def __init__(self, parent: T) -> None:
        """
        初始化子模块并绑定父级宿主对象。

        :param parent: 宿主父类实例对象
        """
        self._parent = parent

    def __getattr__(self, name: str) -> Any:
        """
        处理属性穿透委托，拦截双下划线魔术方法与自引用死循环（当子模块找不到属性时自动代理到 parent）。

        :param name: 待访问的属性名称
        :return: 宿主对象上对应的属性值
        """
        if name.startswith("__") and name.endswith("__"):  # 拦截双下划线魔法方法，防止序列化或反射探测时陷入递归死循环
            raise AttributeError(
                f"{self.__class__.__name__!r} object has no attribute {name!r}"
            )

        if name == "_parent":  # 防止 _parent 未完成初始化时产生循环引用
            raise AttributeError(
                f"{self.__class__.__name__!r} object has no attribute '_parent'"
            )

        try:
            return getattr(self._parent, name)
        except AttributeError:
            raise AttributeError(
                f"Neither {self.__class__.__name__!r} nor its parent "
                f"{self._parent.__class__.__name__!r} has attribute {name!r}"
            ) from None


# ============================== 动态与延迟导入 ==============================

class _LazyModule:
    """
    延迟加载模块代理。

    支持作为“模块级代理对象”与“函数装饰器”双模态运行。
    在真实调用发生前保持静默休眠状态（零导入开销与冷启动耗时），首次访问其属性或
    触发被装饰函数执行时，通过双重检查锁（DCL）安全加载底层模块并缓存。
    """

    def __init__(
            self,
            module_name: str,
            pip_name: str | None = None,
            extra_modules: Sequence[str] = (),
            as_name: str | None = None,
    ) -> None:
        """
        初始化延迟加载代理对象。

        :param module_name: 待加载的核心模块完整路径（如 'win32gui', 'numpy'）
        :param pip_name: 对应在 PyPI 上的安装包名；若为 None 则默认提取顶层包名
        :param extra_modules: 需要同步校验并导入的关联依赖列表
        :param as_name: 装饰器模式下注入至全局命名空间的变量名
        """
        self.__dict__["_module_name"] = module_name  # 直接操作 __dict__，绕过 __setattr__，否则在初始化完成前会触发无限递归死循环
        self.__dict__["_pip_name"] = pip_name or module_name.split(".")[0]
        self.__dict__["_extra_modules"] = tuple(extra_modules)
        self.__dict__["_as_name"] = as_name or module_name.split(".")[-1]
        self.__dict__["_real_module"] = None
        self.__dict__["_lock"] = threading.Lock()  # 保护多线程并发导入的互斥锁

    def _load_real_module(self) -> Any:
        """
        执行底层真实模块动态导入与缺失依赖校验（线程安全）。

        :return: 导入完成的原生模块对象
        """
        if self.__dict__["_real_module"] is None:  # 第一重检查：已初始化则直接跳过锁
            with self.__dict__["_lock"]:
                if self.__dict__["_real_module"] is None:  # 第二重检查：获得锁后确认未被其他线程初始化
                    try:
                        mod = importlib.import_module(self.__dict__["_module_name"])
                        for extra in self.__dict__["_extra_modules"]:  # 逐一同步导入并校验伴随扩展依赖
                            importlib.import_module(extra)
                        self.__dict__["_real_module"] = mod
                    except ImportError:  # 组装友好的 pip 安装指引，明确告知缺少的模块名及安装命令
                        pkgs = f"{self.__dict__['_pip_name']} {' '.join(self.__dict__['_extra_modules'])}".strip()
                        raise ImportError(
                            f"Optional dependency '{self.__dict__['_module_name']}' is missing. "
                            f"Install it via: 'pip install {pkgs}'"
                        ) from None

        return self.__dict__["_real_module"]

    # ============================== 描述符与属性代理 ==============================

    def __getattr__(self, name: str) -> Any:
        """
        拦截属性读取操作。首次访问模块属性时触发实际导入。

        :param name: 待获取的属性或方法名称
        :return: 底层模块中对应的属性值
        """
        real_mod = self._load_real_module()  # 触发按需懒加载并取得真实模块
        return getattr(real_mod, name)

    def __setattr__(self, name: str, value: Any) -> None:
        """
        拦截属性写入操作并透传至底层模块。

        :param name: 属性名称
        :param value: 待赋予的属性值
        """
        if "_real_module" not in self.__dict__:  # 若代理对象自身的基础属性尚未完全构建，调用基类原生属性设置
            super().__setattr__(name, value)
            return

        real_mod = self._load_real_module()
        setattr(real_mod, name, value)

    def __dir__(self) -> list[str]:
        """
        透传底层模块的属性清单，支持 IDE 反射探测与动态补全。

        :return: 属性名称列表
        """
        real_mod = self._load_real_module()
        return dir(real_mod)

    def __repr__(self) -> str:
        """
        输出代理对象的调试信息。

        :return: 调试描述字符串
        """
        if self.__dict__["_real_module"] is not None:
            return repr(self.__dict__["_real_module"])
        return f"<LazyModule for '{self.__dict__['_module_name']}' (unloaded)>"

    def __bool__(self) -> bool:
        """
        布尔值检测，始终判定加载状态是否有效。

        :return: True
        """
        return bool(self._load_real_module())

    # ============================== 装饰器调用与模块透传 ==============================

    def __call__(self, target: Any = None, *args: Any, **kwargs: Any) -> Any:
        """
        支持双重调用能力：
            1. 作为函数装饰器使用（首调该函数时将模块实例注入该函数的 globals 作用域）；
            2. 若底层模块自身为可调用对象，直接透传调用。

        :param target: 被装饰的目标函数或透传调用的首个参数
        :param args: 透传位置参数
        :param kwargs: 透传关键字参数
        :return: 装饰后的函数包装器或底层模块调用返回值
        """

        # ------------------------------ 模式 1：作为函数装饰器使用 ------------------------------

        func_globals = getattr(target, "__globals__", None)  # 动态获取全局命名空间字典，避免静态分析器对 Callable 报未解析警告

        if callable(target) and not args and not kwargs and func_globals is not None:
            func = target
            as_name = self.__dict__["_as_name"]

            @wraps(func)
            def wrapper(*f_args: Any, **f_kwargs: Any) -> Any:
                """
                运行时拦截层：在函数首次实际执行时触发导入并挂载至全局变量字典。
                """
                if as_name not in func_globals or isinstance(func_globals[as_name], _LazyModule):
                    func_globals[as_name] = self._load_real_module()  # 检查命名空间并注入已加载的原生模块

                return func(*f_args, **f_kwargs)

            return wrapper

        # ------------------------------ 模式 2：底层真实模块透传调用 ------------------------------

        real_mod = self._load_real_module()
        return real_mod(target, *args, **kwargs) if target is not None else real_mod(*args, **kwargs)


def lazy_import(
        module_name: str,
        *,
        pip_name: str | None = None,
        extra_modules: Sequence[str] = (),
        as_: str | None = None,
) -> Any:
    """
    延迟动态加载可选依赖库工厂函数。

    规避在主流程顶层导入导致的打包体积膨胀与冷启动卡顿。
    具备代理对象直接赋值与函数级装饰器两种调用形态：

    【推荐使用方式一：代理对象顶层赋值（零样板代码、开箱即用）】：
        在模块或类定义最上方直接赋值，代码内直接使用，仅在首次调用属性时触发导入：
        >>> win32gui = lazy_import("win32gui", pip_name="pywin32")
        >>> def test():
        ...     return win32gui.GetForegroundWindow()

    【可选使用方式二：函数级装饰器（首调注入命名空间）】：
        作为装饰器修饰具体函数，在函数首次执行时自动将模块注入 globals 作用域：
        >>> @lazy_import("numpy", as_="np")
        >>> def calculate():
        ...     return np.zeros((3, 3))  # noqa

    :param module_name: 待加载的核心模块完整路径（例如 'win32gui', 'numpy'）
    :param pip_name: 对应在 PyPI 上的安装包名；若为 None 则默认解析使用 module_name 的顶层包名
    :param extra_modules: 需要同步校验并导入的关联依赖列表（例如 ['openpyxl']）
    :param as_: 作为装饰器注入时的变量别名，默认为末级模块名
    :return: 具备懒加载代理能力的 _LazyModule 实例
    :raises ImportError: 当主模块或任何关联模块未安装时抛出
    """
    return _LazyModule(
        module_name=module_name,
        pip_name=pip_name,
        extra_modules=extra_modules,
        as_name=as_,
    )


# ============================== 路径校验 ==============================

def check_path(
        path: str | Path,
        target: Literal["a", "any", "f", "file", "d", "dir"] = "any",
        *,
        error: Literal["i", "ignore", "w", "warn", "r", "raise"] = "i",
) -> Path | None:
    """
    验证路径存在性与类型规范。

    根据目标模式检查文件或目录的合法性，兼具单字符与全拼传参。
    失败时支持静默返回 None、终端警告打印或直接抛出英文异常。

    :param path: 待校验的文件或目录路径
    :param target: 目标类型过滤（'a'/'any': 任意有效路径, 'f'/'file': 文件, 'd'/'dir': 目录）
    :param error: 失败处理策略（'i'/'ignore': 静默返回 None, 'w'/'warn': 终端警告, 'r'/'raise': 抛出异常）
    :return: 校验成功返回 Path 实例，校验未通过返回 None
    """
    error_strategy = {
        "i": "ignore", "ignore": "ignore",
        "w": "warn", "warn": "warn",
        "r": "raise", "raise": "raise",
    }  # 异常策略归一化映射
    mode = error_strategy.get(error.lower() if isinstance(error, str) else error)
    if not mode:
        raise ValueError(f"Invalid error strategy: {error!r}. Expected 'ignore', 'warn', or 'raise'")

    def _fail(msg: str, exc_type: type[Exception] = FileNotFoundError) -> None:
        """
        统一失败处理出口。

        :param msg: 错误描述文本
        :param exc_type: 报错类型
        """
        if mode == "raise":
            raise exc_type(msg)
        if mode == "warn":
            print(msg)

    p = Path(path)

    if not p.exists():  # 路径存在性校验
        _fail(f"Path does not exist: {p}", FileNotFoundError)
        return None

    target_norm = target.lower() if isinstance(target, str) else target  # 目标类型归一化与校验

    if target_norm in ("f", "file"):
        if not p.is_file():
            _fail(f"Path is not a file: {p}", IsADirectoryError)
            return None
    elif target_norm in ("d", "dir"):
        if not p.is_dir():
            _fail(f"Path is not a directory: {p}", NotADirectoryError)
            return None
    elif target_norm not in ("a", "any"):
        raise ValueError(f"Invalid target kind: {target!r}. Expected 'file', 'dir', or 'any'.")

    return p


# ============================== 参数确认 ==============================

def ensure_param(
        param: str,
        converter: Callable[..., Any],
        pass_self: bool = False
) -> Callable:
    """
    高性能参数预转换装饰器。

    在函数构建期预先提取目标形参的位置索引，规避高频遍历中的动态反射损耗。

    :param param: 函数签名中待拦截转换的目标形参名称
    :param converter: 负责执行类型转换的具体回调函数
    :param pass_self: 转换时是否将宿主实例（self）作为额外入参传入
    :return: 包装后的目标函数装饰器闭包
    """

    def decorator(func: Callable) -> Callable:
        """
        装饰器构建层：在模块加载时静态分析函数签名，解析参数位置索引。

        :param func: 被装饰的原生目标函数
        :return: 具有参数预处理能力的包装执行函数
        :raises ValueError: 当目标参数未定义或未提供 self 形参却启用 pass_self 时抛出
        """
        sig = signature(func)
        params = sig.parameters

        if param not in params:
            raise ValueError(f"Parameter {param!r} not found in signature of {func.__qualname__}")

        first_param_name = next(iter(params.keys()), None)  # 若要求传递 self，校验首个形参必须为 'self'
        if pass_self and first_param_name != "self":
            raise ValueError(f"Cannot use pass_self=True on {func.__qualname__}: first parameter is not 'self'")

        target_param = params[param]
        can_be_positional = target_param.kind in (
            Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD
        )

        has_var_positional_before = False
        positional_idx = -1

        for idx, (p_name, p_obj) in enumerate(params.items()):
            if p_name == param:
                positional_idx = idx
                break
            if p_obj.kind == Parameter.VAR_POSITIONAL:
                has_var_positional_before = True
                break

        static_idx = (
            positional_idx if (can_be_positional and not has_var_positional_before) else -1
        )  # 仅当支持位置传参且前方无 *args 时，静态索引有效

        @wraps(func)
        def wrapper(*args, **kwargs):
            """
            运行时拦截层：提取实参并根据静态索引或关键字快速完成类型转换。

            :param args: 传递给目标函数的位置实参元组
            :param kwargs: 传递给目标函数的关键字实参字典
            :return: 目标函数执行后的返回值
            :raises TypeError: 当启用 pass_self 但调用时未能解析到 self 实例时抛出
            """

            self_obj = None  # 严格提取 self 实例
            if pass_self:
                if args:
                    self_obj = args[0]
                elif "self" in kwargs:
                    self_obj = kwargs["self"]
                else:
                    raise TypeError(f"Missing required argument 'self' while invoking {func.__qualname__}")

            if param in kwargs:  # 优先捕获关键字传参
                val = kwargs[param]
                kwargs[param] = converter(val, self_obj) if pass_self else converter(val)

            elif static_idx != -1 and static_idx < len(args):  # 捕获位置实参（使用构建期分析的静态索引直接定位替换）
                args_list = list(args)
                val = args_list[static_idx]
                args_list[static_idx] = converter(val, self_obj) if pass_self else converter(val)
                args = tuple(args_list)

            return func(*args, **kwargs)

        return wrapper

    return decorator
