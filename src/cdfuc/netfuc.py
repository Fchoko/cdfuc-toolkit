import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from .basefuc import DelegatedBase, T, lazy_import
from .logfuc import LogFuc
from .sysfuc import subprocess_run as run
from .timefuc import TimeFuc

# ============================== 延迟加载依赖 ==============================

webdriver = lazy_import("selenium.webdriver", pip_name="selenium")
by_mod = lazy_import("selenium.webdriver.common.by", pip_name="selenium")
keys_mod = lazy_import("selenium.webdriver.common.keys", pip_name="selenium")
ui_mod = lazy_import("selenium.webdriver.support.ui", pip_name="selenium")
ec_mod = lazy_import("selenium.webdriver.support.expected_conditions", pip_name="selenium")
ex_mod = lazy_import("selenium.common.exceptions", pip_name="selenium")


# ============================== 核心门面类 ==============================

class NetFuc:
    """
    统一浏览器管理与自动化工具门面类。

    按需懒加载 Selenium 套件，支持独立实例多开生命周期管理与 Chromium 远程调试接管模式。
    """

    _PROFILE_PATH: str = r"D:\Software\Programs\_Browser_mirror"  # 默认浏览器数据目录

    def __init__(
            self,
            browser: Literal["chrome", "firefox", "edge"] = "chrome",
            headless: bool = False,
            profile_dir: str | Path = _PROFILE_PATH,
            debug_port: int = 0,
    ) -> None:
        """
        初始化浏览器实例。

        :param browser: 浏览器类型，可选 'chrome', 'firefox', 'edge'
        :param headless: 是否启用无头模式
        :param profile_dir: 用户数据持久化存储目录
        :param debug_port: Chromium 远程调试端口（0 为驱动普通拉起，>0 为远程接管模式）
        """
        self._browser = browser.lower()
        self._headless = headless
        self._profile_dir = Path(profile_dir) / self._browser
        self._debug_port = debug_port  # 远程调试端口
        self._localhost = "127.0.0.1"  # 远程调试回环地址

        self._driver: Any = None  # 延迟初始化的 WebDriver 实体

        self.page = _Page(self)  # 挂载子领域代理
        self.element = _Element(self)
        self.session = _Session(self)

        Path(self._profile_dir).mkdir(parents=True, exist_ok=True)

    # ------------------------------ 上下文生命周期管理 ------------------------------

    def __enter__(self) -> "NetFuc":
        """
        上下文管理器进入方法，启动并返回浏览器实例。

        :return: NetFuc 实例对象
        """
        self.launch()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        上下文管理器退出方法，安全关闭浏览器并回收驱动资源。

        :param exc_type: 异常类型
        :param exc_val: 异常实例
        :param exc_tb: 异常追踪栈
        """
        self.quit()

    # ------------------------------ 驱动启动与接管底层 ------------------------------

    def _check_port(self) -> bool:
        """
        判断远程调试端口是否已被本地服务监听占用。

        :return: 端口已开启返回 True，否则返回 False
        """
        if self._debug_port <= 0:  # 表示通过驱动普通拉起，放弃检查端口
            return False

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:  # 通过远程接管模式，检查端口占用情况
            sock.settimeout(1.0)
            return sock.connect_ex((self._localhost, self._debug_port)) == 0

    def _build_options(self):
        """
        构建各浏览器的配置项对象。

        :return: 对应浏览器的 Options 实例
        """
        if self._debug_port > 0:  # 接管模式：构造最小参数集
            if self._browser == "chrome":
                return webdriver.ChromeOptions()
            elif self._browser == "edge":
                return webdriver.EdgeOptions()
            raise RuntimeError(f"Browser '{self._browser}' does not support remote-debug attach mode")

        if self._browser in {"chrome", "edge"}:  # 普通启动模式
            options = webdriver.ChromeOptions() if self._browser == "chrome" else webdriver.EdgeOptions()
            if self._headless:
                options.add_argument("--headless=new")  # 为 options 添加无头参数

            options.add_argument("--mute-audio")  # 静音
            options.add_argument("--disable-gpu")  # 禁用 GPU
            options.add_argument("--disable-extensions")  # 禁用扩展
            options.add_argument("--disable-infobars")  # 禁用信息栏
            options.add_argument("--disable-dev-shm-usage")  # 使用 /tmp 而非 /dev/shm 作为暂存区
            options.add_argument("--no-sandbox")  # 禁用沙盒
            options.add_argument("--ignore-certificate-errors")  # 忽略证书错误
            options.add_argument("--disable-blink-features=AutomationControlled")  # 隐藏浏览器窗口被程序控制的特征
            options.add_argument(f"--user-data-dir={str(self._profile_dir)}")  # 用户数据目录（持久化 cookie 等）
            options.add_argument("--disable-software-rasterizer")  # 禁用软件光栅化

            options.set_capability("goog:loggingPrefs", {"performance": "ALL"})  # 设置性能日志捕获选项
            options.add_experimental_option("detach", True)  # 避免运行完程序后窗口自动关闭
            options.add_experimental_option("excludeSwitches", ["enable-automation"])  # 隐藏浏览器被自动化控制的特征
            options.add_experimental_option(
                "prefs", {"profile.default_content_setting_values.notifications": 2},
            )  # 禁用通知；设置浏览器首选项
            return options

        if self._browser == "firefox":
            options = webdriver.FirefoxOptions()
            if self._headless:
                options.add_argument("-headless")

            options.profile = str(self._profile_dir)  # 指定实际 Firefox 用户资料，而非普通偏好项
            options.set_preference("media.volume_scale", "0.0")  # 静音
            options.set_preference("dom.webnotifications.enabled", False)  # 禁用通知
            options.set_preference("dom.push.enabled", False)
            options.set_preference("webdriver_accept_untrusted_certs", True)  # 忽略证书（根据需要）
            options.set_preference("webdriver_assume_untrusted_issuer", False)
            return options

        raise ValueError(f"Unsupported browser type: '{self._browser}'")

    def _start_chromium_process(self) -> None:
        """
        启动后台 Chromium 远程调试独立进程。
        """
        browser_bins: dict[str, list[str]] = {
            "chrome": [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ],
            "edge": [
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            ],
        }

        exe_path = next((p for p in browser_bins.get(self._browser, []) if Path(p).exists()), None)  # 查找浏览器可执行文件
        if not exe_path:
            raise RuntimeError(f"Unable to locate '{self._browser}' executable in standard system paths")

        if not self._check_port():  # 如果端口未被占用，启动远程调试模式
            args = [
                exe_path,
                f"--remote-debugging-port={self._debug_port}",
                f"--user-data-dir={str(self._profile_dir)}",
                "--no-first-run",  # 跳过首次启动流程
                "--no-default-browser-check",  # 禁止浏览器弹出“是否设为默认浏览器”的提示
            ]
            run(
                args, async_mode=True, creationflags=0x00000010  # 0x00000010 对应 Windows 下的 CREATE_NEW_CONSOLE
            )  # 启动浏览器，创建一个新的控制台窗口
            for _ in range(12):
                TimeFuc.sleep_seconds(0.5)
                if self._check_port():
                    break

    def _create_driver(self):
        """
        初始化并创建底层 WebDriver 实例。

        :return: WebDriver 对象
        """
        options = self._build_options()

        if self._debug_port > 0:  # 端口接管模式
            if self._browser not in {"chrome", "edge"}:
                raise RuntimeError(f"Browser '{self._browser}' does not support remote-debug attach")
            self._start_chromium_process()
            options.add_experimental_option("debuggerAddress", f"{self._localhost}:{self._debug_port}")
            return (
                webdriver.Chrome(options=options)
                if self._browser == "chrome"
                else webdriver.Edge(options=options)
            )

        driver_map = {  # 普通驱动启动模式
            "chrome": (
                webdriver.Chrome,
                "selenium.webdriver.chrome.service",
                "Service",
                "webdriver_manager.chrome",
                "ChromeDriverManager",
            ),
            "firefox": (
                webdriver.Firefox,
                "selenium.webdriver.firefox.service",
                "Service",
                "webdriver_manager.firefox",
                "GeckoDriverManager",
            ),
            "edge": (
                webdriver.Edge,
                "selenium.webdriver.edge.service",
                "Service",
                "webdriver_manager.microsoft",
                "EdgeChromiumDriverManager",
            ),
        }  # 浏览器驱动与配置映射表

        if self._browser not in driver_map:
            raise ValueError(f"Unsupported browser type: '{self._browser}'")

        driver_cls, s_mod, s_name, m_mod, m_name = driver_map[self._browser]
        service_cls = getattr(lazy_import(s_mod), s_name)
        manager_cls = getattr(lazy_import(m_mod), m_name)

        browser = self._browser.capitalize()
        LogFuc.info(f"[{browser}] Verifying driver binary (may take a moment on first run)...")

        try:  # webdriver-manager 会自动比对本地 ~/.wdm 缓存，未命中或需更新时才下载
            driver_path = manager_cls().install()  # 获取 driver 路径
            LogFuc.info(f"[{browser}] Driver ready at: {driver_path}")
            service = service_cls(executable_path=driver_path)
            return driver_cls(service=service, options=options)
        except Exception as e:  # 容灾回退：若 webdriver_manager 发生网络错误，尝试交由 Selenium 内置驱动引擎启动
            LogFuc.warning(f"[{browser}] Failed to fetch via manager: {e}. Falling back to default...")
            return driver_cls(options=options)

    def _safe_call(self, action: Callable[[], T], *, on_fail: T, err_msg: str) -> T:
        """
        执行 WebDriver 操作的统一异常保护封装。

        :param action: 待执行的无参逻辑闭包
        :param on_fail: 失败时的默认返回值
        :param err_msg: 异常报警上下文提示
        :return: 执行结果或 on_fail 默认值
        """
        if not self._driver:
            LogFuc.error("WebDriver is not initialized. Please call launch() first")
            return on_fail

        try:
            return action()
        except Exception as e:
            LogFuc.error(f"{err_msg}: {e}")
            return on_fail

    # ------------------------------ 宿主生命周期调度方法 ------------------------------

    @property
    def driver(self):
        """
        获取底层原生 WebDriver 实例（若未初始化则自动调用 launch 启动）。

        :return: 原生 WebDriver 驱动句柄
        """
        if self._driver is None:
            self.launch()
        return self._driver

    def launch(self, url: str = "", size: tuple[int, int] = (1200, 900)) -> "NetFuc":
        """
        启动浏览器引擎并初始化 WebDriver 会话。

        :param url: 启动后立即打开的初始网址，默认为空
        :param size: 初始窗口宽高元组 (宽, 高)，默认 (1200, 900)
        :return: 当前 NetFuc 实例（支持链式调用）
        """
        if self._driver is None:
            self._driver = self._create_driver()
            TimeFuc.sleep_seconds(1)

        if url:
            self.page.get(url)
        if size:
            self.session.window_set_size(*size)

        return self

    def quit(self) -> None:
        """
        关闭浏览器并彻底释放驱动与系统进程资源。
        """
        if self._driver:
            self._safe_call(
                lambda: self._driver.quit(),
                on_fail=None,
                err_msg="Failed to quit browser",
            )
            self._driver = None


# ============================== 领域委托类 ==============================


class _Page(DelegatedBase[NetFuc]):
    """页面层级控制、导航跳转与整页状态"""

    # ------------------------------ 页面动作 ------------------------------

    def get(self, url: str) -> bool:
        """
        加载并打开指定 URL。

        :param url: 目标网页地址
        :return: 导航成功返回 True，失败返回 False
        """
        # noinspection PyProtectedMember
        return self._parent._safe_call(
            lambda: (self._driver.get(url), True)[1],
            on_fail=False,
            err_msg=f"Failed to open URL '{url}'",
        )

    def refresh(self) -> bool:
        """
        刷新当前网页。

        :return: 刷新成功返回 True，失败返回 False
        """
        # noinspection PyProtectedMember
        return self._parent._safe_call(
            lambda: (self._driver.refresh(), True)[1],
            on_fail=False,
            err_msg="Failed to refresh page",
        )

    def back(self) -> bool:
        """
        在浏览器浏览历史中后退一步。

        :return: 操作成功返回 True，失败返回 False
        """
        # noinspection PyProtectedMember
        return self._parent._safe_call(
            lambda: (self._driver.back(), True)[1],
            on_fail=False,
            err_msg="Failed to go back",
        )

    def forward(self) -> bool:
        """
        在浏览器浏览历史中前进一步。

        :return: 操作成功返回 True，失败返回 False
        """
        # noinspection PyProtectedMember
        return self._parent._safe_call(
            lambda: (self._driver.forward(), True)[1],
            on_fail=False,
            err_msg="Failed to go forward",
        )

    def screenshot(self, file_path: str | Path) -> bool:
        """
        截取当前可视区域并保存至本地文件。

        :param file_path: 目标图片保存物理路径
        :return: 截屏成功返回 True，失败返回 False
        """
        # noinspection PyProtectedMember
        return self._parent._safe_call(
            lambda: bool(self._driver.save_screenshot(str(file_path))),
            on_fail=False,
            err_msg="Failed to take screenshot",
        )

    def js(self, script: str, *args):
        """
        执行自定义 JavaScript 脚本代码。

        :param script: JavaScript 代码字符串
        :param args: 传给脚本的附加参数列表
        :return: 脚本返回值
        """
        return self._driver.execute_script(script, *args)

    # ------------------------------ 页面只读状态 ------------------------------

    def html(self) -> str:
        """
        获取当前网页完整的 HTML DOM 源码。

        :return: HTML 源码字符串
        """
        return self._driver.page_source

    def title(self) -> str:
        """
        获取当前页面的网页标题。

        :return: Title 文本
        """
        return self._driver.title

    def url(self) -> str:
        """
        获取当前浏览器实际所在的 URL 地址。

        :return: 当前页面 URL
        """
        return self._driver.current_url

    def text(self) -> str:
        """
        获取页面 Body 内部所有可见的人类可读文本内容。

        :return: 页面主体可见文本
        """
        return self._driver.execute_script("return document.body.innerText;")

    def ready_state(self) -> str:
        """
        获取文档就绪状态。

        :return: 文档状态字符串（如 'loading', 'interactive', 'complete'）
        """
        return self._driver.execute_script("return document.readyState;")

    def viewport_size(self) -> tuple[int, int]:
        """
        获取页面视口（Viewport）有效渲染尺寸。

        :return: 视口尺寸元组 (宽, 高)
        """
        width, height = self._driver.execute_script("return [window.innerWidth, window.innerHeight];")
        return int(width), int(height)


class _Element(DelegatedBase[NetFuc]):
    """DOM 元素查找、交互与状态查询"""

    @staticmethod
    def _get_by(by: Literal["xpath", "css", "id", "class", "tag", "link", "partial"]):
        """
        转换定位策略字符串为 Selenium 原生 By 枚举。

        :param by: 定位策略名称（如 'xpath', 'css', 'id'）
        :return: By 枚举策略对象
        """
        mapping = {
            "xpath": by_mod.By.XPATH,
            "css": by_mod.By.CSS_SELECTOR,
            "id": by_mod.By.ID,
            "class": by_mod.By.CLASS_NAME,
            "tag": by_mod.By.TAG_NAME,
            "link": by_mod.By.LINK_TEXT,
            "partial": by_mod.By.PARTIAL_LINK_TEXT,
        }
        return mapping.get(by.lower(), by_mod.By.XPATH)

    def _resolve(
            self,
            target: Any,
            action: Callable[[Any], T],
            *,
            by: Literal["xpath", "css", "id", "class", "tag", "link", "partial"] = "xpath",
            default: T | None | bool = None,
    ) -> T | None | bool:
        """
        统一解析输入目标为 WebElement 并在其上执行操作，附带安全容错。

        :param target: WebElement 实例或定位表达式字符串
        :param action: 接收 WebElement 并返回操作结果的闭包
        :param by: 当 target 为字符串时的定位语法
        :param default: 元素未找到或执行失败时的兜底返回值
        :return: action 闭包的返回值或 default
        """
        if hasattr(target, "click"):  # 鸭子类型判断 WebElement
            el = target
        elif isinstance(target, str):
            el = self.find(target, by=by)
        else:
            return default

        if el is None:
            return default

        element_exceptions = (
            ex_mod.StaleElementReferenceException,
            ex_mod.ElementNotInteractableException,
            ex_mod.InvalidElementStateException,
            ex_mod.NoSuchElementException,
            ex_mod.JavascriptException,
        )

        try:
            return action(el)
        except element_exceptions:
            return default

    # ------------------------------ 元素查找与等待 ------------------------------

    def find(
            self,
            locator: str,
            by: Literal["xpath", "css", "id", "class", "tag", "link", "partial"] = "xpath",
    ):
        """
        查找单个匹配的页面元素。

        :param locator: 定位表达式
        :param by: 定位方式
        :return: 匹配的 WebElement 实例，未找到安全返回 None
        """
        try:
            return self._driver.find_element(self._get_by(by), locator)
        except (ex_mod.NoSuchElementException, Exception):
            return None

    def find_all(
            self,
            locator: str,
            by: Literal["xpath", "css", "id", "class", "tag", "link", "partial"] = "xpath",
    ) -> list:
        """
        查找所有匹配的页面元素列表。

        :param locator: 定位表达式
        :param by: 定位方式
        :return: 匹配的 WebElement 列表，未找到返回空列表
        """
        locate_exceptions = (
            ex_mod.InvalidSelectorException,
            ex_mod.NoSuchWindowException,
            ex_mod.StaleElementReferenceException,
            ex_mod.WebDriverException,
        )

        try:
            return self._driver.find_elements(self._get_by(by), locator)
        except locate_exceptions:
            return []

    def wait_for(
            self,
            keyword: str,
            *,
            by: Literal["xpath", "css", "id", "class", "tag", "link", "partial"] = "xpath",
            timeout: int = 10,
    ):
        """
        显式阻塞等待元素挂载到 DOM 树中。

        :param keyword: 定位表达式
        :param by: 定位方式
        :param timeout: 最大超时时长（秒）
        :return: 成功匹配返回 WebElement，超时返回 None
        """
        try:
            return ui_mod.WebDriverWait(self._driver, timeout).until(
                ec_mod.presence_of_element_located((self._get_by(by), keyword))
            )
        except (ex_mod.TimeoutException, Exception):
            return None

    # ------------------------------ 元素行为交互 ------------------------------

    def click(
            self,
            target: Any,
            *,
            by: Literal["xpath", "css", "id", "class", "tag", "link", "partial"] = "xpath",
    ) -> bool:
        """
        点击指定的页面元素。

        :param target: WebElement 或定位表达式
        :param by: 定位方式
        :return: 点击成功返回 True，失败返回 False
        """
        return bool(self._resolve(
            target,
            action=lambda el: (el.click(), True)[1],
            by=by,
            default=False,
        ))

    def input(
            self,
            target: Any,
            text: str,
            *,
            clear: bool = True,
            by: Literal["xpath", "css", "id", "class", "tag", "link", "partial"] = "xpath",
    ) -> bool:
        """
        向输入框元素输入指定文本。

        :param target: WebElement 或定位表达式
        :param text: 待输入的字符串
        :param clear: 是否在键入前清空内容，默认 True
        :param by: 定位方式
        :return: 输入成功返回 True，失败返回 False
        """
        if clear and not self.clear(target, by=by):
            return False  # 未能清空旧内容时终止输入，避免将文本意外追加

        return bool(self._resolve(
            target,
            action=lambda el: (el.send_keys(text), True)[1],
            by=by,
            default=False,
        ))

    def clear(
            self,
            target: Any,
            *,
            by: Literal["xpath", "css", "id", "class", "tag", "link", "partial"] = "xpath",
    ) -> bool:
        """
        清空输入框已有的文本内容。

        :param target: WebElement 或定位表达式
        :param by: 定位方式
        :return: 清空成功返回 True，失败返回 False
        """
        return bool(self._resolve(
            target,
            action=lambda el: (el.clear(), True)[1],
            by=by,
            default=False,
        ))

    def background_click(
            self,
            target: Any,
            *,
            by: Literal["xpath", "css", "id", "class", "tag", "link", "partial"] = "xpath",
    ) -> bool:
        """
        通过 Ctrl+Enter 模拟点击并在后台新建标签页打开。

        :param target: WebElement 或定位表达式
        :param by: 定位方式
        :return: 执行成功返回 True，失败返回 False
        """
        return bool(self._resolve(
            target,
            action=lambda el: (el.send_keys(keys_mod.Keys.CONTROL, keys_mod.Keys.ENTER), True)[1],
            by=by,
            default=False,
        ))

    def scroll_to(
            self,
            target: Any,
            *,
            by: Literal["xpath", "css", "id", "class", "tag", "link", "partial"] = "xpath",
    ) -> bool:
        """
        通过平滑滚动将目标元素置于视口中央。

        :param target: WebElement 或定位表达式
        :param by: 定位方式
        :return: 滚动成功返回 True，失败返回 False
        """
        return bool(self._resolve(
            target,
            action=lambda el: (
                self._driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el),
                True,
            )[1],
            by=by,
            default=False,
        ))

    # ------------------------------ 元素状态与信息 ------------------------------

    def text(
            self,
            target: Any,
            *,
            by: Literal["xpath", "css", "id", "class", "tag", "link", "partial"] = "xpath",
    ) -> str | None:
        """
        获取指定元素包含的文本内容。

        :param target: WebElement 或定位表达式
        :param by: 定位方式
        :return: 元素文本字符串，失败返回 None
        """
        return self._resolve(
            target,
            action=lambda el: el.text,
            by=by,
            default=None,
        )

    def attr(
            self,
            target: Any,
            name: str,
            *,
            by: Literal["xpath", "css", "id", "class", "tag", "link", "partial"] = "xpath",
    ) -> str | None:
        """
        读取指定元素的 HTML 属性值。

        :param target: WebElement 或定位表达式
        :param name: 属性名称
        :param by: 定位方式
        :return: 属性值字符串，失败返回 None
        """
        return self._resolve(
            target,
            action=lambda el: el.get_attribute(name),
            by=by,
            default=None,
        )

    def rect(
            self,
            target: Any,
            *,
            by: Literal["xpath", "css", "id", "class", "tag", "link", "partial"] = "xpath",
    ) -> dict[str, int] | None:
        """
        获取元素在当前视口中的绝对几何位置与尺寸信息。

        :param target: WebElement 或定位表达式
        :param by: 定位方式
        :return: 包含 x, y, width, height 的字典，失败返回 None
        """
        return self._resolve(
            target,
            action=lambda el: el.rect,
            by=by,
            default=None,
        )

    def is_displayed(
            self,
            target: Any,
            *,
            by: Literal["xpath", "css", "id", "class", "tag", "link", "partial"] = "xpath",
    ) -> bool:
        """
        判断元素当前在页面中是否可见。

        :param target: WebElement 或定位表达式
        :param by: 定位方式
        :return: 可见返回 True，不可见或未找到返回 False
        """
        return bool(self._resolve(
            target,
            action=lambda el: el.is_displayed(),
            by=by,
            default=False,
        ))

    def is_enabled(
            self,
            target: Any,
            *,
            by: Literal["xpath", "css", "id", "class", "tag", "link", "partial"] = "xpath",
    ) -> bool:
        """
        判断元素当前是否处于可交互激活状态（未设置 disabled）。

        :param target: WebElement 或定位表达式
        :param by: 定位方式
        :return: 处于激活可用状态返回 True，否则返回 False
        """
        return bool(self._resolve(
            target,
            action=lambda el: el.is_enabled(),
            by=by,
            default=False,
        ))


class _Session(DelegatedBase[NetFuc]):
    """窗口管理、环境上下文与会话凭据"""

    # ------------------------------ 窗口尺寸与形态控制 ------------------------------

    def window_size(self) -> tuple[int, int]:
        """
        获取当前主窗口的外边框尺寸 (宽, 高)。

        :return: 窗口像素尺寸元组 (宽, 高)
        """
        sz = self._driver.get_window_size()
        return sz["width"], sz["height"]

    def window_set_size(self, width: int, height: int) -> bool:
        """
        设定当前浏览器主窗口的宽高大小。

        :param width: 窗口目标宽度（像素）
        :param height: 窗口目标高度（像素）
        :return: 调整成功返回 True，失败返回 False
        """
        # noinspection PyProtectedMember
        return self._parent._safe_call(
            lambda: (self._driver.set_window_size(width, height), True)[1],
            on_fail=False,
            err_msg="Failed to set window size",
        )

    def window_maximize(self) -> bool:
        """
        将浏览器主窗口最大化显示。

        :return: 最大化成功返回 True，失败返回 False
        """
        # noinspection PyProtectedMember
        return self._parent._safe_call(
            lambda: (self._driver.maximize_window(), True)[1],
            on_fail=False,
            err_msg="Failed to maximize window",
        )

    def window_minimize(self) -> bool:
        """
        将浏览器主窗口最小化至任务栏。

        :return: 最小化成功返回 True，失败返回 False
        """
        # noinspection PyProtectedMember
        return self._parent._safe_call(
            lambda: (self._driver.minimize_window(), True)[1],
            on_fail=False,
            err_msg="Failed to minimize window",
        )

    def window_fullscreen(self) -> bool:
        """
        将浏览器主窗口切换为全屏（F11）模式。

        :return: 全屏成功返回 True，失败返回 False
        """
        # noinspection PyProtectedMember
        return self._parent._safe_call(
            lambda: (self._driver.fullscreen_window(), True)[1],
            on_fail=False,
            err_msg="Failed to enter fullscreen",
        )

    # ------------------------------ 多窗口与句柄切换 ------------------------------

    def window_handles(self) -> list[str]:
        """
        获取当前浏览器实例开启的所有窗口句柄列表。

        :return: 窗口句柄字符串列表
        """
        return self._driver.window_handles

    def current_window(self) -> str:
        """
        获取当前正处于激活焦点的窗口句柄。

        :return: 激活窗口句柄字符串
        """
        return self._driver.current_window_handle

    def switch_to_window(self, handle: str) -> bool:
        """
        根据窗口句柄切换当前操作上下文至对应标签页/窗口。

        :param handle: 目标窗口句柄
        :return: 切换成功返回 True，失败返回 False
        """
        # noinspection PyProtectedMember
        return self._parent._safe_call(
            lambda: (self._driver.switch_to.window(handle), True)[1],
            on_fail=False,
            err_msg="Failed to switch window",
        )

    # ------------------------------ Cookie 凭据与环境元数据 ------------------------------

    def cookies(self) -> list[dict]:
        """
        获取当前站点上下文加载的所有 Cookie 字典列表。

        :return: Cookie 字典列表
        """
        return self._driver.get_cookies()

    def cookie(self, name: str) -> dict | None:
        """
        根据名称检索并返回单个指定的 Cookie。

        :param name: Cookie 字段键名
        :return: 匹配的 Cookie 字典，不存在返回 None
        """
        return self._driver.get_cookie(name)

    def add_cookie(self, cookie_dict: dict) -> bool:
        """
        向当前会话注入单个 Cookie 数据项。

        :param cookie_dict: 符合 WebDriver 格式规范的 Cookie 字典
        :return: 注入成功返回 True，失败返回 False
        """
        # noinspection PyProtectedMember
        return self._parent._safe_call(
            lambda: (self._driver.add_cookie(cookie_dict), True)[1],
            on_fail=False,
            err_msg="Failed to add cookie",
        )

    def delete_cookie(self, name: str) -> bool:
        """
        根据指定键名删除对应的 Cookie 记录。

        :param name: 待删除的 Cookie 键名
        :return: 删除成功返回 True，失败返回 False
        """
        # noinspection PyProtectedMember
        return self._parent._safe_call(
            lambda: (self._driver.delete_cookie(name), True)[1],
            on_fail=False,
            err_msg=f"Failed to delete cookie '{name}'",
        )

    def clear_cookies(self) -> bool:
        """
        清除当前作用域域名下的全部 Cookies 凭据。

        :return: 清理成功返回 True，失败返回 False
        """
        # noinspection PyProtectedMember
        return self._parent._safe_call(
            lambda: (self._driver.delete_all_cookies(), True)[1],
            on_fail=False,
            err_msg="Failed to clear cookies",
        )

    def user_agent(self) -> str:
        """
        读取当前浏览器运行时的真实 User-Agent 请求头标识。

        :return: User-Agent 字符串
        """
        return self._driver.execute_script("return navigator.userAgent;")
